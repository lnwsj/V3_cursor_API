"""App startup/shutdown lifecycle (FIX Phase 4)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from app.backend.services.db import init_schema as _init_schema
from app.backend.services.workers import init_workers

if TYPE_CHECKING:
    pass  # noqa: F401  (only for type hints; FastAPI imported above)

log = logging.getLogger("v3-gateway")


async def _reconcile_active_jobs() -> None:
    """Stub for startup recovery — full impl lives in services.jobs (FIX Phase 4)."""
    try:
        from app.backend.services.jobs import list_active_jobs
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
