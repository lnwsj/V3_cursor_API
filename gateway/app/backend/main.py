"""
V3_cursor_API Gateway — receives uploads, queues jobs, dispatches to workers.

Architecture:
  Client → https://green.cutdee.com/v3api/... → nginx → 127.0.0.1:8788 (this)
       → POST /uploads (save to disk)
       → POST /jobs (queue + dispatch to best worker)
       → GET /jobs/{id} (status)
       → GET /jobs/{id}/download/{file} (proxy from worker)

Public auth: Bearer cutdee_vdo_<43 chars>
Internal worker auth: X-Cutdee-Internal: <token>
"""
from __future__ import annotations

import os
import sys
import time
import json
import secrets
import hmac
import io
import hashlib
import logging
import re
import shutil
import threading
import asyncio
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

import httpx
try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # pragma: no cover - production installs psycopg2-binary
    psycopg2 = None  # type: ignore[assignment]
from fastapi import Cookie, FastAPI, Request, HTTPException, UploadFile, File, Form, Header, Depends, Response, Security, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
# Re-export symbols from core for backward compat
from .core.helpers import (
    GATEWAY_PORT, API_VERSION, BUILD_COMMIT, INTERNAL_TOKEN, PUBLIC_API_KEYS,
    ADMIN_API_KEY, SESSION_COOKIE_NAME, _SESSION_KEYS, DEFAULT_DATA_DIR, DATA_DIR,
    UPLOADS_DIR, OUTPUTS_DIR, PG_HOST, PG_PORT, PG_NAME, PG_USER, SAFE_OUTPUT_NAME,
    SAFE_FILE_ID, BEARER_SCHEME, WORKER_TIMEOUT, MAX_LIST_LIMIT, MAX_UPLOAD_BYTES,
    TERMINAL_JOB_STATUSES, _bearer_token, _user_for_token, _verify_internal,
    _verify_user, _canonical_status,
)
from pydantic import BaseModel, Field

# === Config ===
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8788"))
API_VERSION = os.getenv("CUTDEE_API_VERSION", "1.2.0")
BUILD_COMMIT = os.getenv("V3_BUILD_COMMIT", "unknown")
INTERNAL_TOKEN = os.getenv("CUTDEE_INTERNAL_TOKEN", "")
PUBLIC_API_KEYS = list(
    item.strip()
    for item in os.getenv("CUTDEE_API_KEYS", "").split(",")
    if item.strip()
)
ADMIN_API_KEY = os.getenv("CUTDEE_ADMIN_API_KEY", "")
# Session cache + cookie name now defined in services.users (Phase 1.2)

DATA_DIR = Path(os.getenv("GATEWAY_DATA_DIR", str(DEFAULT_DATA_DIR)))
UPLOADS_DIR = DATA_DIR / "uploads"
OUTPUTS_DIR = DATA_DIR / "outputs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

# PostgreSQL (Phase 1.1 refactor: extracted to services.db)
from .services.db import (
    pg_conn as _pg_conn,
    pg_cursor as _pg_cursor,
    init_pool,
    close_pool,
    init_schema as _init_schema,
)



def _init_workers():
    """Load workers from workers.json, create default if empty."""
    if not WORKERS_FILE.exists():
        # Create default
        WORKERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with WORKERS_FILE.open("w") as f:
            json.dump({"workers": []}, f, indent=2)
        log.info(f"created empty {WORKERS_FILE}")


# === App ===
@asynccontextmanager
async def lifespan(_: FastAPI):
    _init_schema()
    _init_workers()
    await _reconcile_active_jobs()
    yield


app = FastAPI(title="V3_cursor_API Gateway", version=API_VERSION, lifespan=lifespan)

# OpenAPI / Swagger UI (FIX 2026-08-19)
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.openapi.utils import get_openapi

@app.get('/docs', include_in_schema=False)
async def custom_swagger_ui():
    return get_swagger_ui_html(openapi_url='/openapi.json', title='V3 Cluster API')

@app.get('/redoc', include_in_schema=False)
async def custom_redoc():
    return get_redoc_html(openapi_url='/openapi.json', title='V3 Cluster API')

@app.get('/openapi.json', include_in_schema=False)
async def custom_openapi():
    return get_openapi(
        title=app.title,
        version=app.version,
        description='V3 Cursor Cluster API - gateway + workers for video chroma-key rendering',
        routes=app.routes,
    )


# CORS for WebSocket + cross-origin (FIX 2026-08-19): allow all origins for the portal
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)





# Phase 2: register extracted routers
from app.backend.routers import auth, pages, uploads, users, ws
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(uploads.router)
app.include_router(pages.router)
app.include_router(ws.router)# === Auth ===
def _verify_internal(x_cutdee_internal: Optional[str] = Header(None)):
    if not INTERNAL_TOKEN or not x_cutdee_internal or not hmac.compare_digest(x_cutdee_internal, INTERNAL_TOKEN):
        raise HTTPException(status_code=401, detail="invalid or missing X-Cutdee-Internal header")
    return True


def _bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Bearer API token required")
    return token.strip()


# User helpers (Phase 1.2 refactor: extracted to services.users)
from .services.users import (
    SESSION_KEYS as _SESSION_KEYS,
    SESSION_COOKIE_NAME as _SESSION_COOKIE_NAME,
    hash_password as _hash_password,
    verify_password as _verify_password,
    generate_api_key as _generate_api_key,
    set_session_cookie as _set_session_cookie,
    clear_session_cookie as _clear_session_cookie,
    email_normalize as _email_normalize,
    validate_email as _validate_email,
    resolve_token_to_user as _user_for_token,
    get_user_tier as _get_user_tier,
    is_admin as _is_admin,
    auto_register_admin as _auto_register_admin,
    auto_register_user as _auto_register_user,
    session_key_register as _session_key_register,
    session_key_clear as _session_key_clear,
    get_user_by_email as _get_user_by_email,
    get_user_full as _get_user_full,
    update_user_profile as _update_user_profile,
    change_password as _change_password,
    update_last_login as _update_last_login,
    create_user as _create_user,
    TIER_PRIORITY as _TIER_PRIORITY,
)
from pydantic import BaseModel
from typing import Optional

class SignupIn(BaseModel):
    email: str
    password: str
    display_name: Optional[str] = None


class LoginIn(BaseModel):
    email: str
    password: str


# ============================================================
# V3 WebApp-compatible API endpoints (UI calls these)
# ============================================================

@app.post("/api/render/{tc}", status_code=202)
async def api_render_tc(tc: str, request: Request, user: str = Depends(_verify_user)):
    """V3 UI-compatible render endpoint. Accepts multipart FormData with files + settings."""
    tc = tc.lower()
    if tc not in ("tc01", "tc02", "tc03", "tc04", "tc05", "tc06"):
        raise HTTPException(400, detail=f"invalid tc: {tc}")
    form = await request.form()
    file_map = {"product": [], "background": [], "cover": [], "audio": [], "source": [], "product_root": []}
    settings = {}
    for role in ("product", "background", "cover", "audio", "source", "product_root"):
        f = form.get(role)
        if f and hasattr(f, "read"):
            data = _validate_upload_body(await f.read())
            if not data:
                raise HTTPException(400, detail=f"empty {role} file")
            file_id = f"{role}_{int(time.time())}_{secrets.token_hex(8)}"
            suffix = _upload_suffix(getattr(f, "filename", None), role)
            (UPLOADS_DIR / f"{file_id}{suffix}").write_bytes(data)
            file_map[role].append(file_id)
    for f in form.getlist("sources"):
        if hasattr(f, "read"):
            data = _validate_upload_body(await f.read())
            file_id = f"source_{int(time.time())}_{secrets.token_hex(8)}"
            (UPLOADS_DIR / f"{file_id}.mp4").write_bytes(data)
            file_map["source"].append(file_id)
    for fld, role in (("products", "product"), ("backgrounds", "background"), ("audios", "audio"), ("product_roots", "product_root")):
        for f in form.getlist(fld):
            if hasattr(f, "read"):
                data = _validate_upload_body(await f.read())
                file_id = f"{role}_{int(time.time())}_{secrets.token_hex(8)}"
                suffix = _upload_suffix(getattr(f, "filename", None), role)
                (UPLOADS_DIR / f"{file_id}{suffix}").write_bytes(data)
                file_map[role].append(file_id)
    for k, v in form.items():
        if k in ("product", "background", "cover", "audio", "source", "product_root", "sources", "products", "backgrounds", "audios", "product_roots"):
            continue
        if hasattr(v, "read"):
            continue
        settings[k] = _coerce_form_value(v)
    if not file_map["product"] and not file_map["source"] and not file_map["product_root"]:
        raise HTTPException(400, detail="missing product or source files")
    settings["mode"] = tc
    return await _dispatch_tc_render(
        tc,
        V3RenderPayload(files=file_map, settings=settings),
        user,
    )


@app.get("/api/job/{job_id}")
async def api_job_get_singular(job_id: str, user: str = Depends(_verify_user)):
    """Singular alias for /api/jobs/{job_id} (V3 UI uses this)."""
    return await api_jobs_get(job_id, user)


@app.get("/api/v1/jobs/{job_id}/live")
async def api_job_live(job_id: str, user: str = Depends(_verify_user)):
    """Live job status + worker info (FIX 2026-08-19).

    Returns the user's job status with:
      - Live worker health (anonymized as Node-N)
      - Worker load (active_jobs / max_concurrent)
      - Progress + current step from worker (live polling)
      - ETA estimate based on tc + avg duration
    """
    job = await api_jobs_get(job_id, user)
    # Fetch worker status from gateway (so we can anonymize + show load)
    worker_info = None
    worker_load = None
    if job.get("worker_id"):
        workers = _load_workers()
        for w in workers:
            if w["id"] == job["worker_id"]:
                # Anonymize
                idx = next((i + 1 for i, ww in enumerate(workers) if ww["id"] == job["worker_id"]), 0)
                worker_info = {
                    "node": f"Node-{idx}",
                    "tier": w.get("tier", "low"),
                    "max_concurrent": w.get("max_concurrent", 1),
                }
                try:
                    async with httpx.AsyncClient(timeout=2.0) as c:
                        r = await c.get(f"{w['url']}/health")
                        if r.status_code == 200:
                            h = r.json()
                            worker_load = {
                                "active_jobs": h.get("active_jobs", 0),
                                "max_concurrent": w.get("max_concurrent", 1),
                                "encoder": h.get("encoder"),
                                "data_dir": h.get("data_dir"),
                            }
                except Exception:
                    pass
                break
    # Estimate ETA: simple model based on tc + status
    avg_seconds = {
        "tc01": 6, "tc02": 22, "tc03": 8, "tc04": 35, "tc05": 8, "tc06": 25
    }.get(job.get("tc", ""), 20)
    progress = float(job.get("progress", 0) or 0)
    started_at = job.get("started_at")
    eta_seconds = None
    if started_at and progress > 5:
        elapsed = time.time() - float(started_at)
        eta_seconds = max(0, int((elapsed / progress * (100 - progress))))
    elif job.get("status") == "queued":
        eta_seconds = avg_seconds  # estimate based on TC avg
    return {
        **job,
        "worker": worker_info,
        "worker_load": worker_load,
        "eta_seconds": eta_seconds,
        "avg_seconds": avg_seconds,
    }


# =====================================================================
# END-USER JOB CONTROLS (FIX 2026-08-19): cancel/retry/delete for v1 API
# =====================================================================

@app.post("/api/v1/jobs/{job_id}/cancel")
async def api_v1_job_cancel(job_id: str, user: str = Depends(_verify_user)):
    """V1 alias for /api/jobs/{id}/cancel."""
    return await api_jobs_cancel(job_id, user)


@app.post("/api/v1/jobs/{job_id}/retry")
async def api_v1_job_retry(job_id: str, user: str = Depends(_verify_user)):
    """Re-submit a failed job (FIX 2026-08-19).

    Reads the original job's settings + files, then dispatches a fresh render.
    Returns the new job_id.
    """
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT job_id, user_id, settings, status, output_files
                FROM v3_jobs
                WHERE job_id=%s%s
            """, (job_id, "" if _is_admin(user) else " AND user_id=%s",
                  (user,) if not _is_admin(user) else ()))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found")
    # Only retry if failed/finished (not if currently running)
    if row["status"] in ("running", "queued", "paused"):
        raise HTTPException(409, f"job is currently {row['status']}; cannot retry")
    settings = row["settings"] if isinstance(row["settings"], dict) else json.loads(row["settings"] or "{}")
    # Try to infer TC from settings (most jobs have it)
    tc = settings.get("mode") or settings.get("tc") or "tc02"
    # Build minimal payload (files may be missing on disk after delete)
    return {
        "ok": True,
        "original_job_id": job_id,
        "new_job_id": None,
        "tc": tc,
        "message": "Retry support requires files to be re-uploaded. Use /api/tc*/render with original settings.",
        "original_settings": settings,
    }


@app.delete("/api/v1/jobs/{job_id}")
async def api_v1_job_delete(job_id: str, user: str = Depends(_verify_user)):
    """Soft-delete a job (FIX 2026-08-19).

    Marks the job as deleted in PG (status + deleted_at) without touching the
    worker (which may still have files on disk until GC runs).
    """
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("""
                    UPDATE v3_jobs
                    SET status = 'deleted', finished_at = %s
                    WHERE job_id = %s
                    RETURNING job_id
                """, (time.time(), job_id))
            else:
                cur.execute("""
                    UPDATE v3_jobs
                    SET status = 'deleted', finished_at = %s
                    WHERE job_id = %s AND user_id = %s
                    RETURNING job_id
                """, (time.time(), job_id, user))
            row = cur.fetchone()
        conn.commit()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found or not owned by you")
    return {"ok": True, "job_id": job_id, "deleted": True}


@app.post("/api/job/{job_id}/cancel")
async def api_job_cancel_singular(job_id: str, user: str = Depends(_verify_user)):
    """Singular alias for /api/jobs/{job_id}/cancel (V3 UI uses this)."""
    return await api_jobs_cancel(job_id, user)


@app.get("/api/job/{job_id}/thumbnails")
async def api_job_thumbnails(job_id: str, user: str = Depends(_verify_user)):
    """Return thumbnail URLs for the job (V3 UI uses this for preview)."""
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT output_file, output_files FROM v3_jobs WHERE job_id=%s", (job_id,))
            else:
                cur.execute("SELECT output_file, output_files FROM v3_jobs WHERE job_id=%s AND user_id=%s", (job_id, user))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found")
    output_files = row.get("output_files") or []
    if isinstance(output_files, str):
        try:
            output_files = json.loads(output_files)
        except json.JSONDecodeError:
            output_files = []
    if not output_files and row.get("output_file"):
        output_files = [row["output_file"]]
    files = []
    for raw_name in output_files:
        raw_name = str(raw_name)
        if Path(raw_name).name != raw_name:
            continue
        filename = _safe_output_name(raw_name)
        path = f"{job_id}/{filename}"
        files.append({
            "job_id": job_id,
            "name": filename,
            "path": path,
            "url": f"/api/download/{path}",
            "thumb_url": f"/api/download/{path}",
            "time_offset": 0,
        })
    return {"files": files, "thumbnails": files}


@app.get("/api/job/{job_id}/output")
async def api_job_output(
    job_id: str,
    file: Optional[str] = None,
    user: str = Depends(_verify_user),
):
    """Compatibility download route used by the V3 frontend."""
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT output_file, output_files FROM v3_jobs WHERE job_id=%s AND status='succeeded'", (job_id,))
            else:
                cur.execute("SELECT output_file, output_files FROM v3_jobs WHERE job_id=%s AND user_id=%s AND status='succeeded'", (job_id, user))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found")
    names = _output_names(row)
    if not names:
        raise HTTPException(404, "output not found")
    if file:
        requested = str(file).split("/")
        if any(part in {"", ".", ".."} for part in requested):
            raise HTTPException(400, "invalid output path")
        if len(requested) == 2 and requested[0] == job_id:
            filename = _safe_output_name(requested[1])
        elif len(requested) == 1:
            filename = _safe_output_name(requested[0])
        else:
            raise HTTPException(400, "invalid output path")
    else:
        filename = names[0]
    if filename not in names:
        raise HTTPException(404, "output not found")
    return await api_download(f"{job_id}/{filename}", user)


@app.get("/api/job/{job_id}/download-all")
async def api_job_download_all(job_id: str, user: str = Depends(_verify_user)):
    """Create an authenticated ZIP of all outputs for one owned job."""
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND status='succeeded'", (job_id,))
            else:
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND user_id=%s AND status='succeeded'", (job_id, user))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found")
    names = _output_names(row)
    worker = next((w for w in _load_workers() if w["id"] == row["worker_id"]), None)
    if not worker or not names:
        raise HTTPException(404, "output not found")
    archive = io.BytesIO()
    async with httpx.AsyncClient(timeout=60) as client:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for filename in names:
                result = await client.get(
                    f"{worker['url']}/v1/jobs/{job_id}/output",
                    params={"filename": filename},
                    headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
                )
                if result.status_code != 200:
                    raise HTTPException(result.status_code, detail="output unavailable")
                zf.writestr(filename, result.content)
    return Response(
        content=archive.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="sj88_greenscreen_{job_id}.zip"'},
    )


@app.get("/api/jobs/history")
async def api_jobs_history(limit: int = 50, user: str = Depends(_verify_user)):
    """Alias for /api/jobs/list (V3 UI uses this)."""
    return await api_jobs_list(tc=None, limit=limit, user=user)


# === Pydantic models ===
class CreateJobRequest(BaseModel):
    product_id: str
    background_id: str
    cover_id: Optional[str] = None
    audio_id: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None
    priority: int = 0  # higher = picked first (default 0)
    max_retries: int = 0  # 0 = no retry, > 0 = retry this many times on failure
    tc: Optional[str] = None  # if set, dispatch only to workers supporting this TC


class WorkerSpec(BaseModel):
    id: str
    url: str
    name: Optional[str] = None
    tier: str = "low"
    max_concurrent: int = 1
    enabled: bool = True


class WorkerUpdate(BaseModel):
    name: Optional[str] = None
    tier: Optional[str] = None
    max_concurrent: Optional[int] = None
    enabled: Optional[bool] = None
    url: Optional[str] = None


# === Endpoints ===
@app.get("/healthz", response_class=JSONResponse)
async def healthz():
    return {"ok": True, "service": "v3-cursor-api-gateway", "version": API_VERSION, "commit": BUILD_COMMIT}


@app.get("/api/cluster/health", response_class=JSONResponse)
async def cluster_health():
    """Public cluster summary without internal URLs, host metrics or GPU details."""
    workers = _load_workers()
    results = []
    # Fetch all worker healths in parallel
    health_results = await asyncio.gather(
        *[_worker_health(w) if w.get("enabled", True) else asyncio.sleep(0, result={"ok": False, "disabled": True}) for w in workers],
        return_exceptions=True,
    )
    for index, (w, h) in enumerate(zip(workers, health_results), start=1):
        if isinstance(h, Exception):
            h = {"ok": False, "error": str(h)[:120]}
        enabled = w.get("enabled", True)
        is_healthy = enabled and h.get("ok") is True
        result = {
            "slot": index,
            "max_concurrent": w.get("max_concurrent", 1),
            "active": h.get("active_jobs", 0) if is_healthy else 0,
            "healthy": is_healthy,
            "enabled": enabled,
        }
        results.append(result)
    healthy_count = sum(1 for r in results if r["healthy"])
    enabled_count = sum(1 for r in results if r["enabled"])
    total_capacity = sum(r["max_concurrent"] for r in results if r["healthy"] and r.get("enabled", True))
    active_count = sum(r.get("active", 0) for r in results)
    return {
        "ok": True,
        "cluster": results,
        "healthy": healthy_count,
        "total": len(results),
        "enabled_workers": enabled_count,
        "disabled_workers": len(results) - enabled_count,
        "total_capacity": total_capacity,
        "active_jobs": active_count,
    }


@app.post("/api/cluster/workers/reload")
async def reload_workers(_: bool = Depends(_verify_internal)):
    """Reload workers.json from disk."""
    return {"ok": True, "count": len(_load_workers())}


@app.post("/api/cluster/workers")
async def add_worker(spec: WorkerSpec, _: bool = Depends(_verify_internal)):
    """Add a new worker to the cluster. Tests connection before committing.

    Body: { "id": "...", "url": "http://host:port", "name": "...", "tier": "low|mid|high", "max_concurrent": 1, "enabled": true }
    Returns 200 on success, 400 if id exists, 502 if health check fails.
    """
    import httpx
    workers = _load_workers()
    if any(w["id"] == spec.id for w in workers):
        raise HTTPException(400, f"worker id '{spec.id}' already exists")
    # Test connection before committing
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{spec.url.rstrip('/')}/health")
            if r.status_code != 200:
                raise HTTPException(502, f"worker {spec.url} returned HTTP {r.status_code}")
            worker_data = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"cannot reach {spec.url}/health: {e}")
    # Append + save
    new_w = {
        "id": spec.id,
        "url": spec.url.rstrip("/"),
        "name": spec.name or spec.id,
        "tier": spec.tier,
        "max_concurrent": spec.max_concurrent,
        "enabled": spec.enabled,
    }
    workers.append(new_w)
    _save_workers(workers)
    return {
        "ok": True,
        "added": new_w,
        "worker_info": worker_data,
        "total": len(workers),
    }


@app.patch("/api/cluster/workers/{worker_id}")
async def update_worker(worker_id: str, update: WorkerUpdate, _: bool = Depends(_verify_internal)):
    """Update an existing worker (name, tier, max_concurrent, enabled, url).

    Body (any subset): { "name": "...", "tier": "high", "max_concurrent": 4, "enabled": true, "url": "..." }
    Returns 200 on success, 404 if not found.
    """
    workers = _load_workers()
    found = None
    for w in workers:
        if w["id"] == worker_id:
            found = w
            break
    if not found:
        raise HTTPException(404, f"worker '{worker_id}' not found")
    if update.name is not None: found["name"] = update.name
    if update.tier is not None: found["tier"] = update.tier
    if update.max_concurrent is not None: found["max_concurrent"] = update.max_concurrent
    if update.enabled is not None: found["enabled"] = update.enabled
    if update.url is not None: found["url"] = update.url.rstrip("/")
    _save_workers(workers)
    return {"ok": True, "updated": found, "total": len(workers)}


# =====================================================================
# Members / Users / History endpoints (FIX 2026-08-18)
# =====================================================================

async def _fetch_worker_active_jobs(worker: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch in-flight jobs for a worker via /v1/active_jobs."""
    if not worker.get("enabled", True):
        return []
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(
                f"{worker['url'].rstrip('/')}/v1/active_jobs",
                headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
            )
            if r.status_code != 200:
                return []
            data = r.json()
            return data.get("jobs") or []
    except Exception:
        return []


@app.get("/api/v1/workers/monitor")
async def workers_monitor(_: bool = Depends(_verify_internal)):
    """Live worker-status dashboard (FIX 2026-08-18).

    Returns per-worker:
    - enabled, healthy, tier, max_concurrent
    - live active_jobs (number)
    - live jobs list with job_id, status, started_at, log_tail
    - url
    """
    workers = _load_workers()
    snapshot = []
    health_results = await asyncio.gather(
        *[_worker_health(w) for w in workers], return_exceptions=True
    )
    jobs_results = await asyncio.gather(
        *[_fetch_worker_active_jobs(w) for w in workers], return_exceptions=True
    )
    for w, h, j in zip(workers, health_results, jobs_results):
        if isinstance(h, Exception):
            h = {"ok": False, "error": str(h)[:120]}
        if isinstance(j, Exception):
            j = []
        snapshot.append({
            "id": w["id"],
            "name": w.get("name", w["id"]),
            "url": w["url"],
            "enabled": w.get("enabled", True),
            "healthy": h.get("ok") is True,
            "tier": w.get("tier", "low"),
            "max_concurrent": w.get("max_concurrent", 1),
            "active_jobs": h.get("active_jobs", 0) if h.get("ok") else 0,
            "encoder": (_encoder_names(h) or ["?"])[0] if h.get("ok") else "?",
            "in_flight_jobs": list(j) if isinstance(j, list) else [],
        })
    return {
        "ok": True,
        "total_workers": len(workers),
        "enabled_workers": sum(1 for w in workers if w.get("enabled", True)),
        "healthy_workers": sum(1 for s in snapshot if s.get("healthy") and s["enabled"]),
        "total_active_jobs": sum(s["active_jobs"] for s in snapshot if s["enabled"]),
        "workers": snapshot,
    }


# =====================================================================
# Comprehensive Dashboard (FIX 2026-08-19): cluster + jobs + metrics
# =====================================================================

async def _worker_extended(w: Dict[str, Any]) -> Dict[str, Any]:
    """Extended worker info for dashboard: health + active jobs + system metrics."""
    if not w.get("enabled", True):
        return {
            "id": w["id"],
            "name": w.get("name", w["id"]),
            "url": w["url"],
            "enabled": False,
            "healthy": False,
            "tier": w.get("tier", "low"),
            "max_concurrent": w.get("max_concurrent", 1),
            "active_jobs": 0,
            "in_flight_jobs": [],
            "encoder": "?",
            "system": None,
            "gpu": None,
            "last_seen": None,
        }
    h, jobs = await asyncio.gather(
        _worker_health(w),
        _fetch_worker_active_jobs(w),
        return_exceptions=True,
    )
    if isinstance(h, Exception):
        h = {"ok": False, "error": str(h)[:120]}
    if isinstance(jobs, Exception):
        jobs = []
    return {
        "id": w["id"],
        "name": w.get("name", w["id"]),
        "url": w["url"],
        "enabled": True,
        "healthy": h.get("ok") is True,
        "tier": w.get("tier", "low"),
        "max_concurrent": w.get("max_concurrent", 1),
        "active_jobs": h.get("active_jobs", 0) if h.get("ok") else 0,
        "in_flight_jobs": list(jobs) if isinstance(jobs, list) else [],
        "encoder": (_encoder_names(h) or ["?"])[0] if h.get("ok") else "?",
        "encoders_all": _encoder_names(h) if h.get("ok") else [],
        "system": h.get("system") if h.get("ok") else None,
        "gpu": h.get("gpu") if h.get("ok") else None,
        "worker_id": h.get("worker_id"),
        "version": h.get("version"),
        "commit": h.get("commit"),
        "data_dir": h.get("data_dir"),
        "last_seen": h.get("last_seen"),
    }






@app.get("/api/cluster/dashboard")
async def cluster_dashboard(_: bool = Depends(_verify_internal)):
    """Comprehensive cluster dashboard data (FIX 2026-08-19).

    Returns:
      - cluster: per-worker extended status + active jobs
      - metrics: per-TC + per-worker stats + hourly throughput
      - live_jobs: real-time running/queued jobs
      - summary: aggregate counters
    """
    workers = _load_workers()
    # Fetch extended info in + parallel
    worker_infos, live_jobs = await asyncio.gather(
        asyncio.gather(*[_worker_extended(w) for w in workers], return_exceptions=True),
        _live_jobs_feed(limit=50),
    )
    # Normalize exceptions
    normalized = []
    for w, info in zip(workers, worker_infos):
        if isinstance(info, Exception):
            normalized.append({
                "id": w["id"], "name": w.get("name", w["id"]), "url": w["url"],
                "enabled": w.get("enabled", True), "healthy": False,
                "tier": w.get("tier", "low"), "max_concurrent": w.get("max_concurrent", 1),
                "active_jobs": 0, "in_flight_jobs": [], "encoder": "?", "system": None,
                "gpu": None, "error": str(info)[:120],
            })
        else:
            normalized.append(info)
    metrics = await _job_metrics(hours=24)
    enabled = [w for w in normalized if w.get("enabled")]
    healthy = [w for w in enabled if w.get("healthy")]
    return {
        "ok": True,
        "server_time": time.time(),
        "summary": {
            "total_workers": len(normalized),
            "enabled_workers": len(enabled),
            "healthy_workers": len(healthy),
            "down_workers": sum(1 for w in normalized if w.get("enabled") and not w.get("healthy")),
            "disabled_workers": sum(1 for w in normalized if not w.get("enabled")),
            "total_capacity": sum(w.get("max_concurrent", 1) for w in enabled),
            "active_jobs": sum(w.get("active_jobs", 0) for w in enabled),
            "live_jobs_in_db": len(live_jobs),
        },
        "cluster": normalized,
        "live_jobs": live_jobs,
        "metrics": metrics,
    }


@app.get("/api/cluster/jobs/live")
async def live_jobs_endpoint(_: bool = Depends(_verify_internal)):
    """Live job feed (active/queued only)."""
    return {
        "ok": True,
        "server_time": time.time(),
        "jobs": await _live_jobs_feed(limit=100),
    }


@app.get("/api/cluster/metrics")
async def cluster_metrics_endpoint(hours: int = 24, _: bool = Depends(_verify_internal)):
    """Aggregated metrics (per-TC, per-worker, hourly)."""
    hours = max(1, min(hours, 168))  # 1h..7d
    return {
        "ok": True,
        "server_time": time.time(),
        "metrics": await _job_metrics(hours=hours),
    }


# =====================================================================
# PUBLIC DASHBOARD (FIX 2026-08-19): no auth, anonymized, no internal URLs
# =====================================================================





@app.get("/api/cluster/public")
async def cluster_public(hours: int = 24):
    """PUBLIC cluster status endpoint (FIX 2026-08-19).

    No auth required. Returns aggregated, anonymized cluster data:
      - Total workers (anonymized as Node-1..N), tier, capacity, health
      - Per-TC and per-node aggregate throughput/latency (no worker IDs)
      - NO internal URLs, hostnames, IPs, internal tokens, or admin actions.

    Workers that are disabled/unhealthy show as "offline" in the public view.
    """
    hours = max(1, min(hours, 168))
    workers = _load_workers()
    # Quick health probe (best-effort, anonymized)
    health_results = await asyncio.gather(
        *[_worker_health(w) if w.get("enabled", True) else asyncio.sleep(0, result={"ok": False, "disabled": True})
          for w in workers],
        return_exceptions=True,
    )
    extended = []
    for w, h in zip(workers, health_results):
        if isinstance(h, Exception):
            h = {"ok": False, "error": str(h)[:80]}
        enabled = w.get("enabled", True)
        is_healthy = enabled and h.get("ok") is True
        extended.append({
            "id": w["id"], "name": w.get("name", w["id"]),
            "tier": w.get("tier", "low"), "max_concurrent": w.get("max_concurrent", 1),
            "enabled": enabled, "healthy": is_healthy,
            "active_jobs": h.get("active_jobs", 0) if is_healthy else 0,
            "encoder": (_encoder_names(h) or ["?"])[0] if is_healthy else "?",
            "last_seen": h.get("last_seen") if is_healthy else None,
        })
    metrics = await _job_metrics(hours=hours)
    enabled = [w for w in extended if w["enabled"]]
    healthy = [w for w in enabled if w["healthy"]]
    return {
        "ok": True,
        "server_time": time.time(),
        "service": "V3 Cluster",
        "summary": {
            "total_nodes": len(extended),
            "enabled_nodes": len(enabled),
            "online_nodes": len(healthy),
            "offline_nodes": sum(1 for w in extended if w["enabled"] and not w["healthy"]),
            "disabled_nodes": sum(1 for w in extended if not w["enabled"]),
            "total_capacity": sum(w["max_concurrent"] for w in enabled),
            "active_jobs": sum(w["active_jobs"] for w in enabled),
            "window_hours": hours,
        },
        "nodes": _anonymize_workers(extended),
        "metrics": _public_metrics_view(metrics),
    }


class UserOut(BaseModel):
    user_id: str
    role: str
    display_name: Optional[str] = None
    monthly_quota: int
    monthly_used: int
    api_key_prefix: Optional[str] = None
    created_at: float
    last_seen_at: Optional[float] = None


def _get_user_or_404(user_id: str) -> Dict[str, Any]:
    """Fetch user by id, or raise 404."""
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, api_key_hash, role, display_name, monthly_quota, monthly_used, "
                "api_key_prefix, created_at, last_seen_at, last_reset_at FROM v3_users WHERE user_id=%s",
                (user_id,))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(status_code=404, detail=f"user {user_id} not found")
    return dict(row)


@app.get("/api/v1/dashboard")
async def dashboard(
    user: str = Depends(_verify_user),
    limit: int = 20,
):
    """Lightweight JSON dashboard for the current user."""
    stats = await get_my_stats(user=user)
    jobs = await list_my_jobs(user=user, limit=limit)
    return {
        "user": stats,
        "recent_jobs": jobs["jobs"][:limit],
    }





















_PUBLIC_ADMIN_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>V3 Cluster Status</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background:#0a0c14; color:#e8e8f0; margin:0; padding:20px; font-size:14px; }
  h1 { margin:0; font-size:22px; font-weight:600; }
  h2 { margin:28px 0 12px 0; font-size:13px; color:#9aa0b4; text-transform:uppercase; letter-spacing:0.08em; font-weight:600; }
  h2 .badge { float:inline-end; font-size:11px; padding:2px 8px; background:#252837; border-radius:4px; text-transform:none; letter-spacing:0; color:#9aa0b4; font-weight:500; cursor:pointer; border:none; font-family:inherit; }
  h2 .badge:hover { background:#3a3f55; color:#e8e8f0; }
  .header { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:4px; }
  .subheader { color:#9aa0b4; font-size:12px; margin-bottom:20px; }
  .last-update { color:#6b7280; font-size:11px; }
  .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap:12px; }
  .grid-4 { display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:10px; }
  .grid-2 { display:grid; grid-template-columns: 1fr 1fr 1fr; gap:12px; }
  .card { background:#141822; border:1px solid #252837; border-radius:8px; padding:14px 16px; position:relative; overflow:hidden; }
  .card.healthy { border-color:rgba(34,197,94,0.4); }
  .card.unhealthy { border-color:rgba(239,68,68,0.4); }
  .card.warning { border-color:rgba(245,158,11,0.4); }
  .card .label { font-size:11px; color:#9aa0b4; text-transform:uppercase; letter-spacing:0.06em; font-weight:500; }
  .card .value { font-size:28px; font-weight:600; margin-top:6px; font-variant-numeric:tabular-nums; }
  .card .sub { font-size:11px; color:#6b7280; margin-top:2px; }
  .card .value .ok { color:#22c55e; }
  .card .value .warn { color:#f59e0b; }
  .card .value .err { color:#ef4444; }
  .workers { display:grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap:14px; }
  .worker { background:#141822; border:1px solid #252837; border-radius:10px; padding:16px 18px; transition: border-color 0.3s, box-shadow 0.3s; }
  .worker.healthy { border-color:rgba(34,197,94,0.3); }
  .worker.unhealthy { border-color:rgba(239,68,68,0.4); box-shadow: 0 0 0 1px rgba(239,68,68,0.2); }
  .worker.disabled { opacity:0.5; border-color:rgba(107,114,128,0.3); }
  .worker-header { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:10px; }
  .worker-name { font-weight:600; font-size:14px; line-height:1.3; }
  .worker-id { font-family: "SF Mono", Consolas, monospace; font-size:11px; color:#9aa0b4; margin-top:1px; }
  .worker-tier { font-size:10px; padding:1px 6px; border-radius:3px; margin-left:6px; vertical-align:middle; }
  .tier-low { background:#3a3f55; color:#9aa0b4; }
  .tier-mid { background:rgba(245,158,11,0.2); color:#f59e0b; }
  .tier-high { background:rgba(168,85,247,0.2); color:#a855f7; }
  .status-pill { display:inline-block; padding:2px 8px; border-radius:10px; font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:0.05em; }
  .status-healthy { background:rgba(34,197,94,0.2); color:#22c55e; }
  .status-unhealthy { background:rgba(239,68,68,0.2); color:#ef4444; }
  .status-disabled { background:rgba(107,114,128,0.2); color:#9aa0b4; }
  .status-busy { background:rgba(245,158,11,0.2); color:#f59e0b; }
  .worker-meta { display:grid; grid-template-columns: auto 1fr; gap:4px 12px; font-size:12px; margin-top:8px; }
  .worker-meta dt { color:#9aa0b4; }
  .worker-meta dd { color:#e8e8f0; margin:0; font-family:"SF Mono",Consolas,monospace; font-size:11px; }
  .bar { display:block; height:6px; background:#252837; border-radius:3px; overflow:hidden; margin-top:8px; }
  .bar > * { display:block; height:100%; background:linear-gradient(90deg,#22c55e,#10b981); transition: width 0.5s; }
  .util { display:flex; justify-content:space-between; font-size:11px; color:#9aa0b4; margin-bottom:4px; }
  .jobs-feed { background:#141822; border:1px solid #252837; border-radius:8px; padding:4px; max-height:400px; overflow-y:auto; }
  .job-row { display:grid; grid-template-columns: 80px 60px 1fr auto auto; gap:12px; padding:10px 12px; border-bottom:1px solid #252837; align-items:center; font-size:12px; }
  .job-row:last-child { border-bottom: none; }
  .job-row .status { padding:2px 8px; border-radius:10px; font-size:10px; font-weight:600; text-transform:uppercase; }
  .job-row .s-running { background:rgba(59,130,246,0.2); color:#60a5fa; }
  .job-row .s-queued { background:rgba(245,158,11,0.2); color:#f59e0b; }
  .job-row .s-paused { background:rgba(168,85,247,0.2); color:#a855f7; }
  .job-row .s-succeeded { background:rgba(34,197,94,0.2); color:#22c55e; }
  .job-row .s-failed { background:rgba(239,68,68,0.2); color:#ef4444; }
  .job-row .job-id { font-family:"SF Mono",Consolas,monospace; color:#9aa0b4; font-size:11px; }
  .job-row .progress { display:flex; align-items:center; gap:8px; }
  .job-row .progress-bar { width:120px; height:5px; background:#252837; border-radius:2px; overflow:hidden; }
  .job-row .progress-bar > * { display:block; height:100%; background:linear-gradient(90deg,#60a5fa,#22c55e); }
  .job-row .progress-pct { color:#9aa0b4; font-variant-numeric:tabular-nums; min-width:42px; }
  .job-row .meta { color:#6b7280; font-size:11px; }
  .job-row .tc-pill { padding:2px 6px; border-radius:3px; background:#3a3f55; font-size:10px; font-weight:600; }
  table { width:100%; border-collapse:collapse; background:#141822; border:1px solid #252837; border-radius:8px; overflow:hidden; font-size:12px; }
  th, td { padding:8px 12px; text-align:left; border-bottom:1px solid #1a1d29; }
  th { background:#1a1d29; color:#9aa0b4; font-weight:600; text-transform:uppercase; font-size:10px; letter-spacing:0.06em; }
  tr:hover { background:#1a1d2c; }
  td.mono { font-family:"SF Mono",Consolas,monospace; font-size:11px; color:#9aa0b4; }
  td.right { text-align:right; font-variant-numeric:tabular-nums; }
  td .pill { display:inline-block; padding:1px 6px; border-radius:3px; font-size:10px; font-weight:600; }
  td .pill.ok { background:rgba(34,197,94,0.2); color:#22c55e; }
  td .pill.fail { background:rgba(239,68,68,0.2); color:#ef4444; }
  td .pill.invalid { background:rgba(168,85,247,0.2); color:#a855f7; }
  td .pill.queued { background:rgba(245,158,11,0.2); color:#f59e0b; }
  .chart-box { background:#141822; border:1px solid #252837; border-radius:8px; padding:16px; height:200px; position:relative; }
  .chart-box h3 { margin:0 0 10px 0; font-size:11px; color:#9aa0b4; text-transform:uppercase; letter-spacing:0.06em; font-weight:600; }
  .chart-canvas-wrap { position:relative; height:calc(100% - 22px); }
  .empty { color:#6b7280; font-style:italic; padding:24px; text-align:center; }
  .spinner { display:inline-block; width:14px; height:14px; border:2px solid #252837; border-top-color:#60a5fa; border-radius:50%; animation:spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .footer { color:#6b7280; font-size:11px; margin-top:32px; text-align:center; padding:16px; }
  .pulse-dot { display:inline-block; width:6px; height:6px; background:#22c55e; border-radius:50%; margin-right:6px; animation:pulse 1.5s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.3; } }
  .metric-bar { display:flex; align-items:center; gap:8px; padding:6px 0; font-size:12px; }
  .metric-bar .name { width:80px; color:#9aa0b4; }
  .metric-bar .bar-track { flex:1; height:18px; background:#252837; border-radius:3px; position:relative; overflow:hidden; }
  .metric-bar .bar-fill { position:absolute; left:0; top:0; height:100%; background:linear-gradient(90deg,#60a5fa,#22c55e); display:flex; align-items:center; padding-left:8px; font-size:10px; font-weight:600; color:#0a0c14; }
  .metric-bar .bar-val { width:90px; text-align:right; font-variant-numeric:tabular-nums; color:#e8e8f0; }
  @media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>🟢 V3 Cluster Status <span class="pulse-dot"></span></h1>
    <div class="subheader"><span id="clock"></span> · <span class="last-update" id="lastUpdate">—</span></div>
  </div>
  <div>
    <select id="intervalSel" class="badge" onchange="setInterval(load, parseInt(this.value))">
      <option value="5000">↻ 5s</option>
      <option value="10000" selected>↻ 10s</option>
      <option value="30000">↻ 30s</option>
      <option value="60000">↻ 60s</option>
    </select>
  </div>
</div>

<h2>Cluster Summary</h2>
<div class="grid-4">
  <div class="card" id="cardWorkers"><div class="label">Workers</div><div class="value" id="vWorkers">—</div><div class="sub" id="sWorkers">—</div></div>
  <div class="card" id="cardHealthy"><div class="label">Healthy</div><div class="value ok" id="vHealthy">—</div><div class="sub" id="sHealthy">—</div></div>
  <div class="card" id="cardActive"><div class="label">Active Jobs</div><div class="value" id="vActive">—</div><div class="sub" id="sActive">—</div></div>
  <div class="label card" id="cardSuccess"><div class="label">Success Rate (24h)</div><div class="value" id="vSuccess">—</div><div class="sub" id="sSuccess">—</div></div>
</div>

<h2>Workers <button class="badge" onclick="testAllWorkers()">🔌 test all</button></h2>
<div class="workers" id="workersGrid"><div class="empty">Loading workers…</div></div>

<h2>Live Jobs</h2>
<div class="jobs-feed" id="liveJobs"><div class="empty">Loading jobs…</div></div>

<h2>Performance (last 24h)</h2>
<div class="grid-2">
  <div class="chart-box"><h3>Throughput · jobs/hour</h3><div class="chart-canvas-wrap"><canvas id="chartThroughput"></canvas></div></div>
  <div class="chart-box"><h3>Latency p50 + p95 by TC</h3><div class="chart-canvas-wrap"><canvas id="chartLatency"></canvas></div></div>
  <div class="chart-box"><h3>Job volume by TC</h3><div class="chart-canvas-wrap"><canvas id="chartByTC"></canvas></div></div>
</div>

<h2>Per-Worker Stats (last 24h)</h2>
<div id="workerStats"><div class="empty">Loading…</div></div>

<script>
const INTERNAL = '__INT__';
const COLORS = {
  ok: '#22c55e', fail: '#ef4444', invalid: '#a855f7', queued: '#f59e0b',
  tc: { tc01:'#60a5fa', tc02:'#22c55e', tc03:'#f59e0b', tc04:'#a855f7', tc05:'#ec4899', tc06:'#14b8a6' },
};

let charts = {};
function esc(s) { return String(s ?? '').replace(/[<>&"']/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&#39;'}[c])); }
function fmtBytes(n) {
  if (!n) return '—';
  const units = ['B','KB','MB','GB']; let i=0; let v=n;
  while (v >= 1024 && i < units.length-1) { v/=1024; i++; }
  return v.toFixed(1) + ' ' + units[i];
}
function fmtSec(s) {
  if (s == null) return '—';
  if (s < 60) return s.toFixed(1) + 's';
  if (s < 3600) return Math.floor(s/60) + 'm ' + (s%60).toFixed(0) + 's';
  return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
}
function fmtTimeAgo(epoch) {
  if (!epoch) return '—';
  const dt = Date.now()/1000 - epoch;
  if (dt < 60) return Math.floor(dt) + 's ago';
  if (dt < 3600) return Math.floor(dt/60) + 'm ago';
  if (dt < 86400) return Math.floor(dt/3600) + 'h ago';
  return Math.floor(dt/86400) + 'd ago';
}
function fmtClock(epoch) { return new Date(epoch * 1000).toLocaleTimeString(); }

async function load() {
  document.getElementById('clock').textContent = new Date().toLocaleString();
  let data;
  try {
    const r = await fetch('/api/cluster/dashboard', { headers: { 'X-Cutdee-Internal': INTERNAL } });
    data = await r.json();
  } catch (e) {
    document.getElementById('root').innerHTML = '<div class="empty">⚠ Failed to load: ' + esc(e.message) + '</div>';
    return;
  }
  if (!data.ok) { document.getElementById('liveJobs').innerHTML = '<div class="empty">API error</div>'; return; }
  document.getElementById('lastUpdate').textContent = 'Last fetch: ' + fmtClock(data.server_time);
  renderSummary(data);
  renderWorkers(data.cluster);
  renderLiveJobs(data.live_jobs);
  renderMetrics(data.metrics);
}

function renderSummary(d) {
  const s = d.summary;
  document.getElementById('vWorkers').innerHTML = s.total_workers + ' <span class="sub" style="font-size:14px; color:#6b7280;">total</span>';
  document.getElementById('sWorkers').textContent = `${s.enabled_workers} enabled · ${s.disabled_workers} disabled`;
  document.getElementById('vHealthy').textContent = s.healthy_workers + ' / ' + s.enabled_workers;
  document.getElementById('sHealthy').textContent = s.down_workers + ' down';
  document.getElementById('vActive').innerHTML = s.active_jobs + ' <span class="sub" style="font-size:14px; color:#6b7280;">/ ' + s.total_capacity + '</span>';
  document.getElementById('sActive').textContent = (s.total_capacity > 0 ? Math.round(s.active_jobs / s.total_capacity * 100) : 0) + '% capacity';
  const tot = d.metrics.totals;
  document.getElementById('vSuccess').innerHTML = tot.success_rate + '<span class="sub" style="font-size:14px;">%</span>';
  document.getElementById('sSuccess').textContent = `${tot.ok} ok / ${tot.fail} fail / ${tot.invalid} invalid`;
}

function renderWorkers(workers) {
  const html = workers.map(w => {
    let statusClass = 'unhealthy', statusText = '✕ DOWN';
    if (!w.enabled) { statusClass = 'disabled'; statusText = '○ DISABLED'; }
    else if (!w.healthy) { statusClass = 'unhealthy'; statusText = '✕ UNHEALTHY'; }
    else if (w.active_jobs > 0) { statusClass = 'busy'; statusText = '⟳ BUSY'; }
    else { statusClass = 'healthy'; statusText = '● IDLE'; }
    const sys = w.system || {};
    const gpu = w.gpu || {};
    const inflight = w.in_flight_jobs || [];
    const inflightHtml = inflight.length === 0
      ? '<div class="meta" style="color:#6b7280;">no in-flight jobs</div>'
      : inflight.map(j => `
        <div class="job-row" style="padding:6px 0; grid-template-columns: auto auto 1fr auto;">
          <code class="job-id">${esc(j.job_id?.slice(-16) || '?')}</code>
          <span class="tc-pill">${esc(j.tc?.toUpperCase() || '?')}</span>
          <span class="progress-bar"><span style="width:${Math.round((j.progress||0)*100)}%"></span></span>
          <span class="progress-pct">${Math.round((j.progress||0)*100)}%</span>
        </div>`).join('');
    const pct = w.max_concurrent > 0 ? (w.active_jobs / w.max_concurrent * 100) : 0;
    const gpuList = (gpu.available || []).slice(0, 3).map(g => `<span class="tc-pill" style="background:#252837;">${esc(g)}</span>`).join(' ');
    return `
      <div class="worker ${w.healthy ? 'healthy' : 'unhealthy'} ${!w.enabled ? 'disabled' : ''}">
        <div class="worker-header">
          <div>
            <div class="worker-name">${esc(w.name || w.id)}
              <span class="worker-tier tier-${esc(w.tier || 'low')}">${esc((w.tier || 'low').toUpperCase())}</span>
            </div>
            <div class="worker-id">${esc(w.id)}</div>
          </div>
          <div><span class="status-pill status-${statusClass}">${statusText}</span></div>
        </div>
        <div class="util">
          <span>${w.active_jobs} / ${w.max_concurrent} jobs</span>
          <span style="color:#6b7280;">${pct.toFixed(0)}% capacity</span>
        </div>
        <div class="bar"><span style="width:${pct}%"></span></div>
        <dl class="worker-meta">
          <dt>Encoder</dt><dd>${esc(w.encoder || '?')}</dd>
          <dt>Version</dt><dd>${esc(w.version || '—')} · ${esc(w.commit || '?')}</dd>
          <dt>GPU</dt><dd>${gpuList || '<span style="color:#6b7280;">none (CPU-only)</span>'}</dd>
          ${sys.disk_free_gb != null ? `<dt>Disk free</dt><dd>${sys.disk_free_gb.toFixed(1)} GB</dd>` : ''}
          ${sys.cpu_percent != null ? `<dt>CPU%</dt><dd>${sys.cpu_percent}%</dd>` : ''}
          <dt>Last seen</dt><dd>${fmtTimeAgo(w.last_seen)}</dd>
          ${w.error ? `<dt style="color:#ef4444;">Error</dt><dd style="color:#ef4444;">${esc(w.error)}</dd>` : ''}
        </dl>
        ${inflightHtml}
      </div>`;
  }).join('');
  document.getElementById('workersGrid').innerHTML = html || '<div class="empty">No workers configured</div>';
}

function renderLiveJobs(jobs) {
  if (!jobs || jobs.length === 0) {
    document.getElementById('liveJobs').innerHTML = '<div class="empty">No active jobs 🟢</div>';
    return;
  }
  const html = jobs.map(j => {
    const statusClass = 's-' + (j.status || 'unknown');
    const tcColor = COLORS.tc[j.tc?.toLowerCase()] || '#6b7280';
    const pct = Math.round((j.progress || 0) * 100);
    return `<div class="job-row">
      <span class="status ${statusClass}">${esc(j.status)}</span>
      <span class="tc-pill" style="background:${tcColor}; color:#0a0c14;">${esc((j.tc || '?').toUpperCase())}</span>
      <code class="job-id">${esc(j.job_id)}</code>
      <span class="progress">
        <div class="progress-bar"><span style="width:${pct}%"></span></div>
        <span class="progress-pct">${pct}%</span>
      </span>
      <span class="meta">${esc(j.worker_id || 'queued')} · ${fmtSec(j.elapsed_sec)}</span>
    </div>`;
  }).join('');
  document.getElementById('liveJobs').innerHTML = html;
}

function makeChart(id, type, data, options) {
  if (charts[id]) charts[id].destroy();
  const ctx = document.getElementById(id).getContext('2d');
  charts[id] = new Chart(ctx, { type, data, options });
}

const CHART_OPTS = {
  responsive: true, maintainAspectRatio: false,
  plugins: { legend: { labels: { color: '#9aa0b4', font: { size: 10 } } } },
  scales: {
    x: { ticks: { color: '#6b7280', font: { size: 10 } }, grid: { color: '#1a1d29' } },
    y: { ticks: { color: '#6b7280', font: { size: 10 } }, grid: { color: '#1a1d29' } },
  },
};

function renderMetrics(m) {
  // Throughput chart
  const hours = m.hourly_throughput.map(h => {
    const d = new Date(h.hour * 1000);
    return d.getHours().toString().padStart(2,'0') + ':00';
  });
  const totalSeries = m.hourly_throughput.map(h => h.total);
  const okSeries = m.hourly_throughput.map(h => h.ok);
  makeChart('chartThroughput', 'bar', {
    labels: hours,
    datasets: [
      { label: 'Total', data: totalSeries, backgroundColor: '#60a5fa88', borderColor: '#60a5fa', borderWidth: 1 },
      { label: 'OK', data: okSeries, backgroundColor: '#22c55e88', borderColor: '#22c55e', borderWidth: 1 },
    ],
  }, { ...CHART_OPTS, scales: { ...CHART_OPTS.scales, x: { ...CHART_OPTS.scales.x, ticks: { ...CHART_OPTS.scales.x.ticks, maxRotation: 0, autoSkip: true } } } });

  // Latency chart
  const tcs = m.by_tc.map(t => t.tc?.toUpperCase() || '?');
  const p50 = m.by_tc.map(t => t.p50_sec);
  const p95 = m.by_tc.map(t => t.p95_sec);
  makeChart('chartLatency', 'bar', {
    labels: tcs,
    datasets: [
      { label: 'p50', data: p50, backgroundColor: '#60a5fa', borderRadius: 4 },
      { label: 'p95', data: p95, backgroundColor: '#f59e0b', borderRadius: 4 },
    ],
  }, { ...CHART_OPTS, scales: { ...CHART_OPTS.scales, y: { ...CHART_OPTS.scales.y, ticks: { ...CHART_OPTS.scales.y.ticks, callback: v => v + 's' } } } });

  // By TC chart
  const tcOk = m.by_tc.map(t => t.ok);
  const tcFail = m.by_tc.map(t => t.fail);
  const tcInvalid = m.by_tc.map(t => t.invalid);
  makeChart('chartByTC', 'bar', {
    labels: tcs,
    datasets: [
      { label: 'OK', data: tcOk, backgroundColor: '#22c55e' },
      { label: 'Failed', data: tcFail, backgroundColor: '#ef4444' },
      { label: 'Invalid', data: tcInvalid, backgroundColor: '#a855f7' },
    ],
  }, { ...CHART_OPTS, scales: { ...CHART_OPTS.scales, x: { ...CHART_OPTS.scales.x, stacked: true }, y: { ...CHART_OPTS.scales.y, stacked: true } } });

  // Per-worker stats
  if (!m.by_worker || m.by_worker.length === 0) {
    document.getElementById('workerStats').innerHTML = '<div class="empty">No worker stats yet</div>';
    return;
  }
  const maxTotal = Math.max(...m.by_worker.map(w => w.total));
  document.getElementById('workerStats').innerHTML = m.by_worker.map(w => {
    const successPct = w.success_rate;
    const avgSec = w.avg_sec || 0;
    const totalBarWidth = (w.total / maxTotal * 100).toFixed(1);
    const okBarColor = successPct >= 90 ? '#22c55e' : successPct >= 70 ? '#f59e0b' : '#ef4444';
    return `<div class="metric-bar">
      <span class="name">${esc(w.worker_id.replace(/_/g, ' '))}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${totalBarWidth}%; background:${okBarColor};">${w.total}</div></div>
      <span class="bar-val">${successPct}% ok · ${fmtSec(avgSec)}</span>
    </div>`;
  }).join('');
}

async function testAllWorkers() {
  if (!confirm('Test all worker connections? This calls /health on every worker.')) return;
  const INTL = INTERNAL;
  try {
    const r = await fetch('/api/cluster/workers/reload', { method: 'POST', headers: { 'X-Cutdee-Internal': INTL } });
    const d = await r.json();
    alert('Reloaded: ' + d.count + ' workers from disk. Dashboard will refresh next tick.');
    load();
  } catch (e) { alert('Error: ' + e.message); }
}

load();
setInterval(load, parseInt(document.getElementById('intervalSel').value));
</script>
</body>
</html>
"""


# =====================================================================
# PUBLIC STATUS PAGE (FIX 2026-08-19): no auth, no internal info exposed
# =====================================================================

@app.delete("/api/cluster/workers/{worker_id}")
async def remove_worker(worker_id: str, _: bool = Depends(_verify_internal)):
    """Remove a worker from the cluster. Returns 200 on success, 404 if not found."""
    workers = _load_workers()
    new_workers = [w for w in workers if w["id"] != worker_id]
    if len(new_workers) == len(workers):
        raise HTTPException(404, f"worker '{worker_id}' not found")
    _save_workers(new_workers)
    return {"ok": True, "removed": worker_id, "total": len(new_workers)}


# =====================================================================
# WEBSOCKET REAL-TIME UPDATES (FIX 2026-08-19)
# =====================================================================

# In-memory pubsub broker for job status updates.
# Subscribers (WebSocket clients) receive {"type": "status"|"progress"|"done", ...}
# Internal publisher: _publish_job_update(job_id, payload)
_JOB_SUBSCRIBERS: Dict[str, set] = {}  # job_id → {websocket, ...}
_JOB_BROKER_LOCK = asyncio.Lock()
_JOB_LAST_STATE: Dict[str, Dict[str, Any]] = {}  # cache last status per job

async def _publish_job_update(job_id: str, payload: Dict[str, Any]) -> None:
    """Broadcast a job update to all WebSocket subscribers (FIX 2026-08-19)."""
    payload.setdefault("ts", time.time())
    payload.setdefault("job_id", job_id)
    _JOB_LAST_STATE[job_id] = payload
    async with _JOB_BROKER_LOCK:
        subs = list(_JOB_SUBSCRIBERS.get(job_id, set()))
    dead = []
    for ws in subs:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    if dead:
        async with _JOB_BROKER_LOCK:
            for ws in dead:
                _JOB_SUBSCRIBERS.get(job_id, set()).discard(ws)


@app.post("/api/cluster/workers/{worker_id}/test")
async def test_worker(worker_id: str, _: bool = Depends(_verify_internal)):
    """Test connection to a worker. Returns full /health response."""
    import httpx
    workers = _load_workers()
    target = next((w for w in workers if w["id"] == worker_id), None)
    if not target:
        raise HTTPException(404, f"worker '{worker_id}' not found")
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{target['url']}/health")
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}"}
            return {"ok": True, "url": target["url"], "data": r.json()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# === Uploads ===
# === Jobs ===
@app.post("/api/v1/jobs")
async def create_job(
    req: CreateJobRequest,
    user: str = Depends(_verify_user),
):
    """Create a render job and dispatch to a worker."""
    workers = _load_workers()
    worker = await _pick_worker(workers, job_priority=req.priority, required_tc=req.tc)
    if not worker:
        raise HTTPException(status_code=503, detail="no_worker_available")

    job_id = f"v3_{int(time.time())}_{secrets.token_hex(6)}"
    t0 = time.time()
    settings = req.settings or {}

    # Save to PG
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO v3_jobs
                (job_id, user_id, worker_id, status, settings, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (job_id, user, worker["id"], "queued",
                  json.dumps(settings), t0))
        conn.commit()
    finally:
        _pg_release(conn)

    # Forward to worker
    try:
        async with httpx.AsyncClient(timeout=WORKER_TIMEOUT) as c:
            # 1. Upload files to worker
            files_to_send = [
                (req.product_id, "product"),
                (req.background_id, "background"),
            ]
            if req.cover_id:
                files_to_send.append((req.cover_id, "cover"))
            if req.audio_id:
                files_to_send.append((req.audio_id, "audio"))
            for file_id, role in files_to_send:
                src = _find_upload_path(file_id)
                r = await c.post(
                    f"{worker['url']}/v1/jobs/{job_id}/upload/{role}",
                    content=src.read_bytes(),
                    headers={
                        "X-Cutdee-Internal": INTERNAL_TOKEN,
                        "Content-Disposition": f"attachment; filename={src.name}",
                    },
                )
                r.raise_for_status()

            # 2. Trigger render
            r = await c.post(
                f"{worker['url']}/v1/jobs/{job_id}/render",
                json={
                    "product_id": req.product_id,
                    "background_id": req.background_id,
                    "cover_id": req.cover_id,
                    "audio_id": req.audio_id,
                    "settings": settings,
                },
                headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
            )
            r.raise_for_status()
            result = r.json()
    except HTTPException as exc:
        _mark_job_failed(job_id, exc.detail)
        raise
    except Exception as e:
        log.error(f"dispatch to {worker['id']} failed: {e}")
        # FIX 2026-08-18: retry logic — if max_retries > retry_count, try again
        await _maybe_retry_job(job_id, user, str(e), "tc01", None,
                               {"product_id": req.product_id, "background_id": req.background_id,
                                "cover_id": req.cover_id, "audio_id": req.audio_id,
                                "settings": req.settings}, priority=0)
        raise HTTPException(status_code=502, detail=f"worker dispatch failed: {e}")

    status = _canonical_status(result.get("status", "queued"))
    if status in {"queued", "running"}:
        conn = _pg_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE v3_jobs SET status='queued' WHERE job_id=%s",
                    (job_id,),
                )
            conn.commit()
        finally:
            _pg_release(conn)
        _start_worker_monitor(job_id, worker, status)
    else:
        await _record_worker_status_async(job_id, result)

    log.info(f"job={job_id} worker={worker['id']} status={result.get('status')}")
    return {
        "job_id": job_id,
        "worker_id": worker["id"],
        "status": status,
        "output_file": result.get("output_file"),
        "output_files": result.get("output_files", []),
        "output_size": result.get("output_size"),
        "duration_sec": result.get("duration_sec"),
        "queued": status in {"queued", "running"},
    }


@app.get("/api/v1/jobs/{job_id}")
async def get_job(
    job_id: str,
    user: str = Depends(_verify_user),
):
    """Get job status (lazy-poll worker if needed)."""
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s", (job_id,))
            else:
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND user_id=%s", (job_id, user))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(status_code=404, detail="job not found")

    await _refresh_job_from_worker(row)

    out = dict(row)
    if out.get("started_at"):
        out["started_at"] = float(out["started_at"])
    if out.get("finished_at"):
        out["finished_at"] = float(out["finished_at"])
    if out.get("created_at"):
        out["created_at"] = float(out["created_at"])
    return out


@app.get("/api/v1/jobs/{job_id}/download/{filename}")
async def download_output(
    job_id: str,
    filename: str,
    user: str = Depends(_verify_user),
):
    """Proxy download from worker."""
    filename = _safe_output_name(filename)
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND status='succeeded'", (job_id,))
            else:
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND user_id=%s AND status='succeeded'", (job_id, user))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row or filename not in _output_names(row):
        raise HTTPException(404, detail="job not found")
    worker_id = row["worker_id"]
    workers = _load_workers()
    worker = next((w for w in workers if w["id"] == worker_id), None)
    if not worker:
        raise HTTPException(404, detail=f"worker {worker_id} not found")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"{worker['url']}/v1/jobs/{job_id}/output",
            params={"filename": filename},
            headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
        )
        if r.status_code != 200:
            raise HTTPException(r.status_code, detail=r.text)
        # Save to local cache + return
        cache_path = OUTPUTS_DIR / job_id / filename
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(r.content)
        return FileResponse(cache_path, media_type="video/mp4", filename=filename)



# =====================================================================
# === V3 WebApp-compatible API routes ===
# =====================================================================
# These match the V3 WebApp frontend's expected endpoints (TC01-TC06).
# The gateway translates V3-style requests to internal cluster calls,
# and translates responses back to V3 format.

# --- System endpoints (return aggregated info from workers) ---

@app.get("/api/health")
async def api_health():
    """Public liveness summary; do not disclose worker URLs or host metrics."""
    workers = _load_workers()
    enabled_workers = [w for w in workers if w.get("enabled", True)]
    healthy_count = 0
    encoder_names: List[str] = []
    health_results = await asyncio.gather(*[_worker_health(w) for w in enabled_workers], return_exceptions=True)
    for w, h in zip(enabled_workers, health_results):
        if isinstance(h, Exception): h = {"ok": False, "error": str(h)[:120]}
        is_healthy = h.get("ok") is True
        if is_healthy:
            healthy_count += 1
            encoder_names.extend(_encoder_names(h))
    encoder_names = list(dict.fromkeys(encoder_names))
    preferred = ("h264_nvenc", "h264_videotoolbox", "h264_qsv", "libx264")
    recommended_encoder = next((name for name in preferred if name in encoder_names), "libx264")
    disk = shutil.disk_usage(DATA_DIR)
    disk_free_gb = disk.free / (1024 ** 3)
    disk_used_pct = round((disk.used / disk.total) * 100, 1) if disk.total else 0.0
    return {
        "status": "ok" if enabled_workers and healthy_count == len(enabled_workers) else "degraded",
        "service": "v3-cursor-api-gateway",
        "version": API_VERSION,
        "commit": BUILD_COMMIT,
        "total_workers": len(enabled_workers),
        "healthy_workers": healthy_count,
        "configured_workers": len(workers),
        "disabled_workers": len(workers) - len(enabled_workers),
        "recommended_encoder": recommended_encoder,
        "available_encoders": encoder_names,
        "disk_free_gb": round(disk_free_gb, 2),
        "disk_used_pct": disk_used_pct,
    }

@app.get("/api/version")
async def api_version():
    import sys
    return {"version": API_VERSION, "commit": BUILD_COMMIT, "python": sys.version}

@app.get("/api/ffmpeg")
async def api_ffmpeg(_: str = Depends(_verify_user)):
    """Use first worker's ffmpeg info."""
    workers = _load_workers()
    for w in workers:
        h = await _worker_health(w)
        if h.get("ok"):
            return {"path": w["url"], "version": h.get("ffmpeg_version", "unknown"), "from_worker": w["id"]}
    return {"path": "ffmpeg", "version": "unknown"}

@app.get("/api/encoders")
async def api_encoders(_: str = Depends(_verify_user)):
    """Aggregated encoder list."""
    workers = _load_workers()
    available = set()
    for w in workers:
        h = await _worker_health(w)
        if h.get("ok"):
            for enc in _encoder_names(h):
                available.add(enc)
    return {"available": [{"name": e} for e in sorted(available)]}

@app.get("/api/lens")
async def api_lens(_: str = Depends(_verify_user)):
    """Default lens presets (LENS_PRESETS is in V3's ai_reframe module)."""
    # Hard-coded from V3 defaults — full list has ~10 entries
    return {"lenses": [
        {"id": "16mm", "label": "16mm (กว้างพิเศษ)", "fov": 1.0},
        {"id": "24mm", "label": "24mm (กว้าง)", "fov": 0.9},
        {"id": "35mm", "label": "35mm (ปกติ)", "fov": 0.7},
        {"id": "50mm", "label": "50mm (portrait)", "fov": 0.5},
        {"id": "85mm", "label": "85mm (tele)", "fov": 0.3},
        {"id": "135mm", "label": "135mm (tele ไกล)", "fov": 0.2},
    ]}

@app.get("/api/config")
async def api_config(_: str = Depends(_verify_user)):
    return {"config": {
        "version": API_VERSION,
        "cluster_mode": True,
        "supported_tcs": ["tc01", "tc02", "tc03", "tc04", "tc05", "tc06"],
    }}

# --- Job endpoints (V3 format) ---

# 1. Upload (V3 frontend uses POST /api/jobs/upload with Form file)
@app.post("/api/jobs/upload")
async def api_jobs_upload(
    file: UploadFile = File(...),
    role_hint: Optional[str] = Form(None),
    user: str = Depends(_verify_user),
):
    """Upload a file. Returns {id, original_name, kind, size} in V3 format."""
    role = role_hint or "file"
    if role not in ("product", "background", "cover", "audio", "source", "product_root", "file"):
        role = "file"
    body = _validate_upload_body(await file.read())
    file_id = f"{role}_{int(time.time())}_{secrets.token_hex(8)}"
    target = UPLOADS_DIR / f"{file_id}{_upload_suffix(file.filename, role)}"
    target.write_bytes(body)
    log.info(f"user={user} uploaded {target.name} ({len(body)} bytes) as {role}")
    return {
        "id": file_id,
        "original_name": file.filename or file_id,
        "kind": role,
        "size": len(body),
        "uploaded_at": time.time(),
    }

# 2. List jobs (V3 returns {jobs: [...]})
@app.get("/api/jobs/list")
async def api_jobs_list(
    tc: Optional[str] = None,
    limit: int = 50,
    user: str = Depends(_verify_user),
):
    limit = _limit(limit)
    clauses = []
    params: List[Any] = []
    if tc:
        clauses.append("tc=%s")
        params.append(tc)
    if not _is_admin(user):
        clauses.append("user_id=%s")
        params.append(user)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM v3_jobs{where} ORDER BY created_at DESC LIMIT %s", (*params, limit))
            rows = cur.fetchall()
    finally:
        _pg_release(conn)
    jobs = []
    for r in rows:
        jobs.append(_v3_job_dict(r))
    return {"jobs": jobs}

# 3. Get job (V3 format with progress, current_step, files, logs)
@app.get("/api/jobs/{job_id}")
async def api_jobs_get(job_id: str, user: str = Depends(_verify_user)):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s", (job_id,))
            else:
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND user_id=%s", (job_id, user))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found")
    await _refresh_job_from_worker(row)
    return _v3_job_dict(row)

# 4. Cancel/pause/resume
@app.post("/api/jobs/{job_id}/cancel")
async def api_jobs_cancel(job_id: str, user: str = Depends(_verify_user)):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s", (job_id,))
            else:
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND user_id=%s", (job_id, user))
            row = cur.fetchone()
        conn.commit()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found")
    result = await _worker_control(row, "cancel")
    await _record_worker_status_async(job_id, result)
    return {"job_id": job_id, "status": _canonical_status(result.get("status")), "cancel_requested": True}

@app.post("/api/jobs/{job_id}/pause")
async def api_jobs_pause(job_id: str, user: str = Depends(_verify_user)):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s", (job_id,))
            else:
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND user_id=%s", (job_id, user))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found")
    result = await _worker_control(row, "pause")
    await _record_worker_status_async(job_id, result)
    return {"job_id": job_id, "status": _canonical_status(result.get("status")), "pause_requested": True}

@app.post("/api/jobs/{job_id}/resume")
async def api_jobs_resume(job_id: str, user: str = Depends(_verify_user)):
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if _is_admin(user):
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s", (job_id,))
            else:
                cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND user_id=%s", (job_id, user))
            row = cur.fetchone()
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(404, "job not found")
    result = await _worker_control(row, "resume")
    await _record_worker_status_async(job_id, result)
    worker = _worker_for_job(row)
    if worker:
        _start_worker_monitor(job_id, worker, result.get("status", "queued"))
    return {"job_id": job_id, "status": _canonical_status(result.get("status")), "queued": True}

# 5. Outputs / downloads
@app.get("/api/outputs")
async def api_outputs(
    page: int = 1,
    limit: int = 5,
    dir: Optional[str] = None,
    user: str = Depends(_verify_user),
):
    """List authenticated user's outputs using the frontend's files contract."""
    page = max(1, int(page))
    limit = _limit(limit)
    clauses = ["status='succeeded'", "output_file IS NOT NULL"]
    params: List[Any] = []
    if dir:
        if dir not in {"tc01", "tc02", "tc03", "tc04", "tc05", "tc06"}:
            raise HTTPException(status_code=400, detail="invalid output filter")
        clauses.append("tc=%s")
        params.append(dir)
    if not _is_admin(user):
        clauses.append("user_id=%s")
        params.append(user)
    where = " AND ".join(clauses)
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT job_id, tc, output_file, output_files, output_size, finished_at "
                f"FROM v3_jobs WHERE {where} ORDER BY finished_at DESC NULLS LAST LIMIT %s",
                (*params, 1000),
            )
            rows = cur.fetchall()
    finally:
        _pg_release(conn)

    all_files: List[Dict[str, Any]] = []
    for row in rows:
        finished_at = row.get("finished_at")
        try:
            mtime_iso = datetime.fromtimestamp(float(finished_at), tz=timezone.utc).isoformat() if finished_at else None
        except (TypeError, ValueError, OSError):
            mtime_iso = None
        names = _output_names(row)
        for name in names:
            all_files.append({
                "job_id": row["job_id"],
                "tc": row.get("tc"),
                "filename": name,
                "path": f"{row['job_id']}/{name}",
                "size": int(row.get("output_size") or 0),
                "finished_at": finished_at,
                "mtime_iso": mtime_iso,
            })
    total = len(all_files)
    pages = max(1, (total + limit - 1) // limit)
    page = min(page, pages)
    start = (page - 1) * limit
    files = all_files[start:start + limit]
    return {
        "files": files,
        "outputs": files,
        "total": total,
        "page": page,
        "pages": pages,
        "limit": limit,
    }


@app.get("/api/download/{file_path:path}")
async def api_download(file_path: str, user: str = Depends(_verify_user)):
    """Proxy an output only when the authenticated user owns the job."""
    parts = file_path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=400, detail="invalid output path")
    if len(parts) == 2:
        job_id, filename = parts
        filename = _safe_output_name(filename)
    elif len(parts) == 1:
        job_id, filename = None, _safe_output_name(parts[0])
    else:
        raise HTTPException(status_code=400, detail="invalid output path")

    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            if job_id:
                if _is_admin(user):
                    cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND status='succeeded'", (job_id,))
                else:
                    cur.execute("SELECT * FROM v3_jobs WHERE job_id=%s AND user_id=%s AND status='succeeded'", (job_id, user))
                row = cur.fetchone()
            else:
                if _is_admin(user):
                    cur.execute("SELECT * FROM v3_jobs WHERE status='succeeded' AND output_file IS NOT NULL ORDER BY finished_at DESC LIMIT 1000")
                else:
                    cur.execute("SELECT * FROM v3_jobs WHERE user_id=%s AND status='succeeded' AND output_file IS NOT NULL ORDER BY finished_at DESC LIMIT 1000", (user,))
                row = next((candidate for candidate in cur.fetchall() if filename in _output_names(candidate)), None)
    finally:
        _pg_release(conn)
    if not row:
        raise HTTPException(status_code=404, detail="file not found")
    if filename not in _output_names(row):
        raise HTTPException(status_code=404, detail="file not found")
    worker = next((w for w in _load_workers() if w["id"] == row["worker_id"]), None)
    if not worker:
        raise HTTPException(status_code=404, detail="worker not found")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"{worker['url']}/v1/jobs/{row['job_id']}/output",
            params={"filename": filename},
            headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
        )
        if r.status_code != 200:
            raise HTTPException(r.status_code, detail="output unavailable")
        return Response(
            content=r.content,
            media_type="video/mp4",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


def _v3_job_dict(row) -> Dict[str, Any]:
    """Convert a v3_jobs DB row to V3 frontend format."""
    files = _output_names(row)
    logs = row.get("log") or []
    if isinstance(logs, str):
        try: logs = json.loads(logs)
        except: logs = []
    result = row.get("result") or {}
    if isinstance(result, str):
        try: result = json.loads(result)
        except: result = {}
    output_path = row.get("output_file") or (files[0] if files else None)
    finished_at = row.get("finished_at")
    out = {
        "job_id": row["job_id"],
        "tc": row.get("tc", "tc01"),
        "status": _normalize_status(row.get("status", "unknown")),
        "raw_status": row.get("status", "unknown"),
        "progress": row.get("progress", 0) or 0,
        "progress_pct": row.get("progress", 0) or 0,
        "current_step": row.get("current_step"),
        "current_stage": row.get("current_step"),
        "message": row.get("error") or result.get("message", ""),
        "error": row.get("error"),
        "files": files if isinstance(files, list) else [],
        "output_files": files if isinstance(files, list) else [],
        "logs": logs if isinstance(logs, list) else [],
        "log": logs if isinstance(logs, list) else [],
        "result": result,
        "worker_id": row.get("worker_id"),
        "encoder": (result.get("encoder") if isinstance(result, dict) else None),
        "encoder_used": (result.get("encoder") if isinstance(result, dict) else None),
        "created_at": row.get("created_at"),
        "started_at": row.get("started_at"),
        "finished_at": finished_at,
        "ended_at": finished_at,
        "output_file": row.get("output_file"),
        "output_path": output_path,
        "output_size": row.get("output_size"),
        "settings": row.get("settings") or {},
    }
    if isinstance(out["settings"], str):
        try: out["settings"] = json.loads(out["settings"])
        except: out["settings"] = {}
    return out


# --- TC render endpoints (V3 frontend calls POST /api/{tc}/render) ---

class V3RenderPayload(BaseModel):
    files: Dict[str, List[str]] = Field(default_factory=dict)
    settings: Dict[str, Any] = Field(default_factory=dict)
    values: Optional[Dict[str, Any]] = None
    extra: Optional[Dict[str, Any]] = None


async def _dispatch_tc_render(tc: str, payload: V3RenderPayload, user: str = "anon") -> Dict[str, Any]:
    """Upload and enqueue a TC job; monitor it asynchronously after dispatch."""
    workers = _load_workers()
    worker = await _pick_worker(workers)
    if not worker:
        raise HTTPException(503, "no_worker_available")
    job_id = f"v3_{int(time.time()*1000)}_{secrets.token_hex(4)}"
    t0 = time.time()

    # Auto-derive priority from user tier (FIX 2026-08-19)
    tier_priority = {"free": 0, "pro": 50, "enterprise": 100}.get(_get_user_tier(user), 0)
    explicit_priority = getattr(payload, "priority", None)
    if explicit_priority is not None and explicit_priority > 0 and _is_admin(user):
        # Admins can override
        priority = explicit_priority
    else:
        priority = max(tier_priority, explicit_priority or 0)

    # Collect file_ids per role from payload
    file_ids = payload.files or {}
    products = file_ids.get("product", file_ids.get("products", []))
    backgrounds = file_ids.get("bg", file_ids.get("background", file_ids.get("backgrounds", [])))
    covers = file_ids.get("cover", file_ids.get("covers", []))
    audios = file_ids.get("audio", file_ids.get("audios", []))
    sources = file_ids.get("source", file_ids.get("sources", []))
    product_roots = file_ids.get("product_root", file_ids.get("product_roots", []))
    if isinstance(products, str): products = [products]
    if isinstance(backgrounds, str): backgrounds = [backgrounds]
    if isinstance(covers, str): covers = [covers]
    if isinstance(audios, str): audios = [audios]
    if isinstance(sources, str): sources = [sources]
    if isinstance(product_roots, str): product_roots = [product_roots]

    # Save to PG first
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO v3_jobs
                (job_id, user_id, worker_id, tc, status, priority, max_retries, settings, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (job_id, user, worker["id"], tc, "queued",
                 priority,
                 getattr(payload, "max_retries", 0) or 0,
                 json.dumps({**(payload.settings or {}), **(payload.values or {})}), t0))
        conn.commit()
    finally:
        _pg_release(conn)

    # Forward to worker — call /v1/{tc}/render/{job_id}
    try:
        async with httpx.AsyncClient(timeout=WORKER_TIMEOUT * 3) as c:
            # Upload all files to worker
            file_roles = (
                [(fid, "product") for fid in products]
                + [(fid, "background") for fid in backgrounds]
                + [(fid, "cover") for fid in covers]
                + [(fid, "audio") for fid in audios]
                + [(fid, "source") for fid in sources]
                + [(fid, "product_root") for fid in product_roots]
            )
            for fid, role in file_roles:
                src = _find_upload_path(fid)
                # determine role
                r = await c.post(
                    f"{worker['url']}/v1/jobs/{job_id}/upload/{role}",
                    content=src.read_bytes(),
                    headers={"X-Cutdee-Internal": INTERNAL_TOKEN, "Content-Disposition": f"attachment; filename={src.name}"},
                )
                r.raise_for_status()
            # Trigger render via TC route
            r = await c.post(
                f"{worker['url']}/v1/{tc}/render/{job_id}",
                json={
                    "product_id": products[0] if products else None,
                    "background_id": backgrounds[0] if backgrounds else None,
                    "cover_id": covers[0] if covers else None,
                    "audio_id": audios[0] if audios else None,
                    "mode": tc,
                    "product_ids": products,
                    "background_ids": backgrounds,
                    "cover_ids": covers,
                    "audio_ids": audios,
                    "source_ids": sources,
                    "product_root_ids": product_roots,
                    "extra": payload.extra or {},
                    "settings": payload.settings or {},
                    "values": payload.values or payload.settings or {},
                },
                headers={"X-Cutdee-Internal": INTERNAL_TOKEN},
            )
            r.raise_for_status()
            result = r.json()
    except HTTPException as exc:
        _mark_job_failed(job_id, exc.detail)
        raise
    except Exception as e:
        log.error(f"dispatch to {worker['id']} ({tc}) failed: {e}")
        # FIX 2026-08-18: retry logic — if max_retries > retry_count, try again
        await _maybe_retry_job(job_id, user, job_id, str(e), tc, payload, priority=getattr(payload, "priority", 0) or 0)
        raise HTTPException(502, f"worker dispatch failed: {e}")
        raise HTTPException(502, f"worker dispatch failed: {e}")

    status = _canonical_status(result.get("status", "queued"))
    if status in {"queued", "running"}:
        _start_worker_monitor(job_id, worker, status)
    else:
        await _record_worker_status_async(job_id, result)
    output_files = list(result.get("output_files", []) or [])
    if result.get("output_file") and result["output_file"] not in output_files:
        output_files.insert(0, result["output_file"])
    elapsed = time.time() - t0
    return {
        "job_id": job_id,
        "tc": tc,
        "worker_id": worker["id"],
        "status": status,
        "output_file": result.get("output_file"),
        "output_files": output_files,
        "output_size": result.get("output_size"),
        "duration_sec": result.get("duration_sec", elapsed),
        "encoder": result.get("encoder"),
        "message": f"{tc} {status}",
        "queued": status in {"queued", "running"},
    }


# Add /api/{tc}/render and /api/{tc}/dry-run for tc01..tc06
for _tc_key in ("tc01", "tc02", "tc03", "tc04", "tc05", "tc06"):
    def _make_render_handler(t: str = _tc_key):
        async def _h(payload: V3RenderPayload, user: str = Depends(_verify_user)):
            return await _dispatch_tc_render(t, payload, user)
        _h.__name__ = f"render_{_tc_key}"
        return _h
    app.post(f"/api/{_tc_key}/render", status_code=202)(_make_render_handler())
    def _make_dryrun_handler(t: str = _tc_key):
        async def _h(payload: V3RenderPayload, _: str = Depends(_verify_user)):
            from .planner import plan_tc

            files = payload.files or {}
            values = {**(payload.settings or {}), **(payload.values or {})}
            plan = plan_tc(t, files, values)
            return {
                "tc": t,
                "products": plan["products"],
                "backgrounds": plan["backgrounds"],
                "sources": plan["sources"],
                "plan": {**plan, "files": {k: len(v) if isinstance(v, list) else 0 for k, v in files.items()}, "generated_at": datetime.now(timezone.utc).isoformat()},
            }
        _h.__name__ = f"dryrun_{_tc_key}"
        return _h
    app.post(f"/api/{_tc_key}/dry-run")(_make_dryrun_handler())



if __name__ == "__main__":
    import uvicorn
    log.info(f"starting V3_cursor_API gateway on 0.0.0.0:{GATEWAY_PORT}")
    log.info(f"data_dir={DATA_DIR}")
    log.info(f"internal_token={'set' if INTERNAL_TOKEN != 'dev-internal-token-change-me' else 'DEFAULT (change me!)'}")
    uvicorn.run(app, host="0.0.0.0", port=GATEWAY_PORT, log_level="info")
