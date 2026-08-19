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
from app.backend.routers import auth, cluster, jobs, pages, system, uploads, users, ws
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(uploads.router)
app.include_router(pages.router)
app.include_router(ws.router)
app.include_router(jobs.router)
app.include_router(cluster.router)
app.include_router(system.router)# === Auth ===
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

# =====================================================================
# END-USER JOB CONTROLS (FIX 2026-08-19): cancel/retry/delete for v1 API
# =====================================================================

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






# =====================================================================
# PUBLIC DASHBOARD (FIX 2026-08-19): no auth, anonymized, no internal URLs
# =====================================================================





# =====================================================================
# PUBLIC STATUS PAGE (FIX 2026-08-19): no auth, no internal info exposed
# =====================================================================

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


# === Uploads ===
# === Jobs ===
# =====================================================================
# === V3 WebApp-compatible API routes ===
# =====================================================================
# These match the V3 WebApp frontend's expected endpoints (TC01-TC06).
# The gateway translates V3-style requests to internal cluster calls,
# and translates responses back to V3 format.

# --- System endpoints (return aggregated info from workers) ---
