"""FastAPI middleware setup (FIX Phase 4)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def install_middleware(app: FastAPI) -> None:
    """Install CORS middleware (FIX 2026-08-19): allow all origins for the portal."""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
