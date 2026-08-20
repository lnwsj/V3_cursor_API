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
    # Backwards-compat shims (FIX 2026-08-20): re-exported so test_gateway_contract
    # and external scripts keep working post-router-refactor.
    _normalize_status, _safe_output_name, _output_names, _coerce_form_value, _find_upload_path,
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
from .routers import auth, cluster, jobs, pages, system, uploads, users, ws
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(uploads.router)
app.include_router(pages.router)
app.include_router(ws.router)
app.include_router(jobs.router)
app.include_router(cluster.router)
app.include_router(system.router)
