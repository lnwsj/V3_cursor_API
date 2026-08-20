"""Shared auth dependencies (Phase 2.1 refactor).

This module holds FastAPI dependency-injection helpers used by every router.
Re-exports common auth primitives from services.users to keep router imports tidy.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from fastapi import Cookie, Depends, Header, HTTPException, Security
from fastapi.security import HTTPBearer

from .services.users import (
    SESSION_COOKIE_NAME,
    TIER_PRIORITY,
    is_admin as _is_admin,
    resolve_token_to_user as _user_for_token,
)


# ---------------------------------------------------------------------------
# File storage (data dir + uploads)
# ---------------------------------------------------------------------------
DEFAULT_DATA_DIR = Path.home() / ".cache" / "v3-cursor-api" / "gateway"
DATA_DIR = Path(os.getenv("GATEWAY_DATA_DIR", str(DEFAULT_DATA_DIR)))
UPLOADS_DIR = DATA_DIR / "uploads"
OUTPUTS_DIR = DATA_DIR / "outputs"
MAX_UPLOAD_BYTES = max(1, int(os.getenv("GATEWAY_MAX_UPLOAD_BYTES", str(200 * 1024 * 1024))))
SAFE_FILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,160}$")
SAFE_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


# Env-driven credentials
ADMIN_API_KEY = os.getenv("CUTDEE_ADMIN_API_KEY", "")
INTERNAL_TOKEN = os.getenv("CUTDEE_INTERNAL_TOKEN", "dev-internal-token-change-me")
PUBLIC_API_KEYS: tuple = tuple(
    item.strip()
    for item in os.getenv("CUTDEE_API_KEYS", "").split(",")
    if item.strip()
)

# Reusable schemes
BEARER_SCHEME = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------
def _bearer_token(authorization: Optional[str]) -> Optional[str]:
    """Extract bare token from 'Authorization: Bearer <token>' header (or None)."""
    if not authorization:
        return None
    token = authorization
    if token.lower().startswith("bearer "):
        token = token[7:]
    return token.strip() or None


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------
def _verify_user(
    authorization: Optional[str] = Header(None),
    cutdee_session: Optional[str] = Cookie(None, alias=SESSION_COOKIE_NAME),
    credentials=Security(BEARER_SCHEME),
):
    """Require a valid bearer token OR the short-lived HttpOnly session cookie."""
    header_value = authorization
    if not header_value and credentials is not None:
        header_value = f"Bearer {credentials.credentials}"
    token = _bearer_token(header_value) if header_value else cutdee_session
    return _user_for_token(token, PUBLIC_API_KEYS, ADMIN_API_KEY)


def _verify_internal(x_cutdee_internal: Optional[str] = Header(None)) -> bool:
    """Require a valid X-Cutdee-Internal header (gateway ↔ worker RPC)."""
    if not INTERNAL_TOKEN or not x_cutdee_internal or x_cutdee_internal != INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing X-Cutdee-Internal header")
    return True


def _get_user_tier(user: str):
    """Proxy: returns the tier string for a user (free/pro/enterprise)."""
    from .services.users import get_user_tier
    return get_user_tier(user)


def _require_admin(user: str = Depends(_verify_user)) -> str:
    """Dependency: require authenticated admin user."""
    if user != "admin":
        raise HTTPException(403, "admin only")
    return user


def _public_tier_boost(tier: str, explicit: Optional[int] = None) -> int:
    """Compute job priority from user tier (+ optional admin override)."""
    base = TIER_PRIORITY.get(tier, 0)
    if explicit is not None and explicit > 0 and _is_admin:
        return max(explicit, base)
    return base
