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

from fastapi import FastAPI

from app.backend.app.lifespan import lifespan
from app.backend.app.middleware import install_middleware
from app.backend.core.helpers import API_VERSION
from app.backend.routers import auth, cluster, jobs, openapi, pages, system, uploads, users, ws

log = logging.getLogger("v3-gateway")
app = FastAPI(title="V3_cursor_API Gateway", version=API_VERSION, lifespan=lifespan)
install_middleware(app)

# Phase 2-4: register extracted routers
for r in (auth, users, uploads, pages, ws, jobs, cluster, system, openapi):
    app.include_router(r.router)
