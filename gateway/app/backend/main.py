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

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi

# Config + auth primitives — single source of truth lives in core.helpers.
# Many of these are re-exported for `gateway.app.backend.main.X` access from
# tests + external scripts; the `# noqa: F401` keeps ruff quiet about the
# otherwise-unused-at-module-scope bindings.
from .core.helpers import (
    ADMIN_API_KEY,  # noqa: F401
    API_VERSION,
    BUILD_COMMIT,  # noqa: F401
    DATA_DIR,
    DEFAULT_DATA_DIR,  # noqa: F401
    GATEWAY_PORT,  # noqa: F401
    INTERNAL_TOKEN,  # noqa: F401
    OUTPUTS_DIR,
    PUBLIC_API_KEYS,  # noqa: F401
    UPLOADS_DIR,
    # Backwards-compat shims (FIX 2026-08-20): test_gateway_contract + external
    # scripts resolve these via `gateway.app.backend.main._normalize_status(...)`.
    _coerce_form_value,  # noqa: F401
    _find_upload_path,  # noqa: F401
    _normalize_status,  # noqa: F401
    _output_names,  # noqa: F401
    _safe_output_name,  # noqa: F401
)
from .services.db import init_schema as _init_schema
from .services.workers import init_workers

# Ensure local data directories exist on startup.
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

# Module logger (FIX 2026-08-20): was undefined after Phase 3 refactor stripped
# the original `log = logging.getLogger("v3-gateway")` declaration.
log = logging.getLogger("v3-gateway")


async def _reconcile_active_jobs() -> None:
    """Stub for startup recovery — full impl lives in services.jobs (TODO Phase 4)."""
    try:
        from .services.jobs import list_active_jobs
        active = list_active_jobs()
        if active:
            log.info("reconcile: %d active jobs", len(active))
    except Exception as exc:  # pragma: no cover - never fatal
        log.warning("reconcile_active_jobs failed: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _init_schema()
    init_workers()
    await _reconcile_active_jobs()
    yield


app = FastAPI(title="V3_cursor_API Gateway", version=API_VERSION, lifespan=lifespan)


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui():
    return get_swagger_ui_html(openapi_url="/openapi.json", title="V3 Cluster API")


@app.get("/redoc", include_in_schema=False)
async def custom_redoc():
    return get_redoc_html(openapi_url="/openapi.json", title="V3 Cluster API")


@app.get("/openapi.json", include_in_schema=False)
async def custom_openapi():
    return get_openapi(
        title=app.title,
        version=app.version,
        description="V3 Cursor Cluster API - gateway + workers for video chroma-key rendering",
        routes=app.routes,
    )


# CORS for WebSocket + cross-origin (FIX 2026-08-19): allow all origins for the portal.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Phase 2: register extracted routers.
from .routers import auth, cluster, jobs, pages, system, uploads, users, ws  # noqa: E402

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(uploads.router)
app.include_router(pages.router)
app.include_router(ws.router)
app.include_router(jobs.router)
app.include_router(cluster.router)
app.include_router(system.router)