"""Core helpers and constants for V3 Cursor API gateway (FIX 2026-08-19).

Extracted from the monolithic 5,233-line main.py to enable a module split
without breaking the existing import surface. main.py imports from this
module so the existing helpers keep working with a single namespace change.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
import asyncio
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import Cookie, Header, HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# === Server config ===
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8788"))
API_VERSION = os.getenv("CUTDEE_API_VERSION", "1.2.0")
BUILD_COMMIT = os.getenv("V3_BUILD_COMMIT", "unknown")
INTERNAL_TOKEN = os.getenv("CUTDEE_INTERNAL_TOKEN", "")
PUBLIC_API_KEYS = list(
    item.strip() for item in os.getenv("CUTDEE_API_KEYS", "").split(",") if item.strip()
)
ADMIN_API_KEY = os.getenv("CUTDEE_ADMIN_API_KEY", "")
SESSION_COOKIE_NAME = "cutdee_session"
_SESSION_KEYS: Dict[str, str] = {}  # api_key -> user_id

# === Paths ===
DEFAULT_DATA_DIR = Path.home() / ".cache" / "v3-cursor-api" / "gateway"
DATA_DIR = Path(os.getenv("GATEWAY_DATA_DIR", str(DEFAULT_DATA_DIR)))
UPLOADS_DIR = DATA_DIR / "uploads"
OUTPUTS_DIR = DATA_DIR / "outputs"

# === DB ===
PG_HOST = os.getenv("CUTDEE_PG_HOST", "127.0.0.1")
PG_PORT = int(os.getenv("CUTDEE_PG_PORT", "6432"))
PG_NAME = os.getenv("CUTDEE_PG_NAME", "v3_cursor_api")
PG_USER = os.getenv("CUTDEE_PG_USER", "v3_cursor_api")

# === Limits ===
SAFE_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
SAFE_FILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,160}$")
BEARER_SCHEME = HTTPBearer(auto_error=False)
WORKER_TIMEOUT = 60.0
MAX_LIST_LIMIT = 100
MAX_UPLOAD_BYTES = max(1, int(os.getenv("GATEWAY_MAX_UPLOAD_BYTES", str(200 * 1024 * 1024))))
TERMINAL_JOB_STATUSES = {"succeeded", "partial", "failed", "cancelled", "paused", "invalid_input"}

# === Helpers ===
def _bearer_token(authorization: Optional[str]) -> Optional[str]:
    """Extract the bearer token from "Authorization: Bearer <token>" header."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[7:]


def _user_for_token(token: Optional[str]) -> str:
    """Resolve the API token to a user_id, auto-registering on first use."""
    if not PUBLIC_API_KEYS:
        raise HTTPException(503, "API authentication not configured")
    if not token or not any(hmac.compare_digest(token, k) for k in PUBLIC_API_KEYS):
        raise HTTPException(401, "invalid API token")
    if ADMIN_API_KEY and hmac.compare_digest(token, ADMIN_API_KEY):
        return "admin"
    user_id = f"u_{hashlib.sha256(token.encode()).hexdigest()[:12]}"
    return user_id


def _verify_internal(x_cutdee_internal: Optional[str] = Header(None)) -> bool:
    """Verify internal worker RPC token."""
    return x_cutdee_internal == INTERNAL_TOKEN


def _verify_user(
    authorization: Optional[str] = Header(None),
    cutdee_session: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME),
    credentials: Optional[HTTPAuthorizationCredentials] = Security(BEARER_SCHEME),
) -> str:
    """Verify API key auth and return user_id."""
    header_value = authorization
    if not header_value and credentials is not None:
        header_value = f"Bearer {credentials.credentials}"
    token = _bearer_token(header_value) if header_value else cutdee_session
    return _user_for_token(token)


def _canonical_status(value: Any) -> str:
    """Normalize ffmpeg status strings to canonical form."""
    raw = str(value or "unknown").strip().lower()
    if raw in {"success", "succeeded", "completed", "done"}:
        return "succeeded"
    if raw in {"failed", "error"}:
        return "failed"
    if raw in {"canceled", "cancelled"}:
        return "cancelled"
    if raw.startswith("invalid") or raw in {"invalid_input", "invalid-input"}:
        return "invalid_input"
    return raw
